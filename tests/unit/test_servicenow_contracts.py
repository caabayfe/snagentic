import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "servicenow" / "app"
FIXTURES = ROOT / "tests" / "fixtures" / "servicenow"


def test_manifest_references_existing_rest_resources() -> None:
    manifest = json.loads((APP / "manifest" / "rest_api.json").read_text(encoding="utf-8"))
    resources = manifest["resources"]
    assert {resource["path"] for resource in resources} >= {
        "/capabilities",
        "/contexts",
        "/domains",
        "/artifacts",
        "/artifacts/export",
        "/preflight",
        "/change-bundles/apply",
        "/diagnostics",
    }
    for resource in resources:
        assert (APP / "src" / "rest_resources" / resource["script"]).is_file()


def test_discovery_fixtures_expose_explicit_contexts_and_types() -> None:
    capabilities = json.loads(
        (FIXTURES / "responses" / "capabilities.json").read_text(encoding="utf-8")
    )
    contexts = json.loads(
        (FIXTURES / "responses" / "contexts-page.json").read_text(encoding="utf-8")
    )
    artifact_types = capabilities["data"]["artifact_types"]
    assert artifact_types
    assert all({"key", "category"} <= item.keys() for item in artifact_types)
    items = contexts["data"]["items"]
    assert items
    assert all(item["domain"]["sys_id"] for item in items)
    assert all(item["application_scope"]["sys_id"] for item in items)


def test_json_artifacts_are_valid_and_sanitized() -> None:
    forbidden_keys = {
        "password",
        "password2",
        "client_secret",
        "api_key",
        "token",
        "private_key",
        "request_body",
        "response_body",
        "comments",
        "work_notes",
        "attachment",
    }
    for path in sorted(APP.rglob("*.json")) + sorted(FIXTURES.rglob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        keys = _keys(payload)
        assert not keys.intersection(forbidden_keys), path


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(map(_keys, value.values())))
    if isinstance(value, list):
        return set().union(*(map(_keys, value)))
    return set()
