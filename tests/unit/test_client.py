from collections.abc import Iterator

import httpx
import pytest
import respx

from snagentic import __version__
from snagentic.client import ServiceNowClient
from snagentic.config import EnvironmentConfig
from snagentic.errors import ServiceNowError


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[EnvironmentConfig]:
    monkeypatch.setenv("TEST_TOKEN", "not-a-real-token")
    yield EnvironmentConfig.model_validate(
        {
            "name": "dev",
            "url": "https://dev.service-now.com/",
            "kind": "development",
            "auth": {"mode": "bearer", "token_env": "TEST_TOKEN"},
        }
    )


@respx.mock
def test_inventory_paginates(environment: EnvironmentConfig) -> None:
    respx.get(
        "https://dev.service-now.com/api/x_snagentic_source/source/v1/capabilities"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "artifact_types": [
                        {"key": "script_include", "category": "managed_bidirectional"}
                    ]
                },
            },
        )
    )
    respx.get(
        "https://dev.service-now.com/api/x_snagentic_source/source/v1/contexts"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "items": [
                        {
                            "domain": {"sys_id": "global"},
                            "application_scope": {"sys_id": "scope-id"},
                        }
                    ],
                    "next_cursor": None,
                },
            },
        )
    )
    route = respx.get(
        "https://dev.service-now.com/api/x_snagentic_source/source/v1/artifacts"
    )
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "ok": True,
                "data": {"items": [{"sys_id": "one"}], "next_cursor": "next"},
            },
        ),
        httpx.Response(
            200,
            json={
                "ok": True,
                "data": {"items": [{"sys_id": "two"}], "next_cursor": None},
            },
        ),
    ]
    with ServiceNowClient(environment) as client:
        assert [item["sys_id"] for item in client.inventory()] == ["one", "two"]
    assert route.call_count == 2


@respx.mock
def test_bearer_auth_uses_resolved_environment_value(
    environment: EnvironmentConfig,
) -> None:
    def assert_authorization(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == f"snagentic/{__version__}"
        assert request.headers["Authorization"] == "Bearer not-a-real-token"
        return httpx.Response(200, json={"ok": True, "data": {}})

    respx.get(
        "https://dev.service-now.com/api/x_snagentic_source/source/v1/capabilities"
    ).mock(side_effect=assert_authorization)
    with ServiceNowClient(environment) as client:
        assert client.capabilities() == {}


@respx.mock
def test_basic_auth_uses_resolved_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SNAGENTIC_DEV_USERNAME", "basic-user")
    monkeypatch.setenv("SNAGENTIC_DEV_PASSWORD", "basic-password")
    environment = EnvironmentConfig.model_validate(
        {
            "name": "dev",
            "url": "https://dev.service-now.com/",
            "kind": "development",
            "auth": {
                "mode": "basic",
                "username_env": "SNAGENTIC_DEV_USERNAME",
                "password_env": "SNAGENTIC_DEV_PASSWORD",
            },
        }
    )

    def assert_authorization(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json={"ok": True, "data": {}})

    respx.get(
        "https://dev.service-now.com/api/x_snagentic_source/source/v1/capabilities"
    ).mock(side_effect=assert_authorization)
    with ServiceNowClient(environment) as client:
        assert client.capabilities() == {}


@respx.mock
def test_errors_do_not_include_response_body(environment: EnvironmentConfig) -> None:
    respx.get(
        "https://dev.service-now.com/api/x_snagentic_source/source/v1/capabilities"
    ).mock(
        return_value=httpx.Response(
            403,
            json={"secret": "must-not-leak"},
            headers={"X-Request-ID": "request-1"},
        )
    )
    with ServiceNowClient(environment) as client, pytest.raises(
        ServiceNowError
    ) as raised:
        client.capabilities()
    assert "request-1" in str(raised.value)
    assert "must-not-leak" not in str(raised.value)


@respx.mock
def test_export_artifact_flattens_companion_envelope(
    environment: EnvironmentConfig,
) -> None:
    route = respx.post(
        "https://dev.service-now.com/api/x_snagentic_source/source/v1/artifacts/export"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "artifacts": [
                        {
                            "artifact_type": "script_include",
                            "sys_id": "1" * 32,
                            "domain": {"sys_id": "global"},
                            "application_scope": {
                                "sys_id": "a" * 32,
                                "scope": "x_example",
                            },
                            "sys_mod_count": 2,
                            "revision": "revision:2",
                            "hash": "0" * 64,
                            "values": {
                                "api_name": "x_example.Utility",
                                "active": "true",
                                "script": "var Utility = Class.create();",
                            },
                        }
                    ]
                },
            },
        )
    )
    inventory = {
        "artifact_type": "script_include",
        "sys_id": "1" * 32,
        "domain": {"sys_id": "global"},
        "application_scope": {"sys_id": "a" * 32},
    }
    with ServiceNowClient(environment) as client:
        artifact = client.export_artifact(inventory)
    assert artifact["scope"] == "x_example"
    assert artifact["stable_key"].startswith("x_example.Utility-")
    assert artifact["code_field"] == "script"
    assert artifact["content"] == "var Utility = Class.create();"
    assert route.calls[0].request.content


@respx.mock
def test_apply_bundle_is_not_retried_and_preserves_safe_details(
    environment: EnvironmentConfig,
) -> None:
    route = respx.post(
        "https://dev.service-now.com/api/x_snagentic_source/source/v1/change-bundles/apply"
    ).mock(
        return_value=httpx.Response(
            503,
            json={
                "ok": False,
                "error": {
                    "code": "partial_failure",
                    "details": [{"failed_index": 1, "completed": [{"index": 0}]}],
                },
            },
        )
    )
    with ServiceNowClient(environment) as client, pytest.raises(
        ServiceNowError
    ) as raised:
        client.apply_bundle({"bundle_id": "b" * 32, "changes": [{}]})
    assert route.call_count == 1
    assert raised.value.code == "partial_failure"
    assert raised.value.details[0]["completed"] == [{"index": 0}]
