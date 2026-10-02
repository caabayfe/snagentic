from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs

import httpx
import pytest

from snagentic import __version__, credentials
from snagentic.config import EnvironmentConfig
from snagentic.errors import ServiceNowError
from snagentic.instance.tableapi import TableApiClient


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


@pytest.fixture
def oauth_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[EnvironmentConfig]:
    monkeypatch.setenv("TEST_CLIENT_ID", "client-id")
    monkeypatch.setenv("TEST_CLIENT_SECRET", "client-secret")
    yield EnvironmentConfig.model_validate(
        {
            "name": "dev",
            "url": "https://dev.service-now.com/",
            "kind": "development",
            "auth": {
                "mode": "oauth",
                "client_id_env": "TEST_CLIENT_ID",
                "client_secret_env": "TEST_CLIENT_SECRET",
                "store": "env",
            },
        }
    )


def test_bearer_auth_and_user_agent_headers(environment: EnvironmentConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer not-a-real-token"
        assert request.headers["User-Agent"] == f"snagentic/{__version__}"
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with TableApiClient(environment, transport=transport) as client:
        res = client.call("GET", "api/now/table/sys_user")
        assert res == {"ok": True}


def test_retry_after_on_429(environment: EnvironmentConfig) -> None:
    responses = [
        httpx.Response(429, headers={"Retry-After": "5"}),
        httpx.Response(200, json={"ok": True}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    sleep_calls: list[int | float] = []
    transport = httpx.MockTransport(handler)
    with TableApiClient(environment, transport=transport, sleep=sleep_calls.append) as client:
        res = client.call("GET", "api/now/table/sys_user")
        assert res == {"ok": True}
    assert sleep_calls == [5.0]


def test_oauth_client_credentials_are_acquired_once_and_reused(
    oauth_environment: EnvironmentConfig,
) -> None:
    token_calls = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, api_calls
        if request.url.path == "/oauth_token.do":
            token_calls += 1
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["client_credentials"],
                "client_id": ["client-id"],
                "client_secret": ["client-secret"],
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        api_calls += 1
        assert request.headers["Authorization"] == "Bearer access-token"
        return httpx.Response(200, json={"result": []})

    transport = httpx.MockTransport(handler)
    with TableApiClient(oauth_environment, transport=transport) as client:
        assert client.call("GET", "api/now/table/sys_user", retries=0) == {"result": []}
        assert client.call("GET", "api/now/table/sys_user_group") == {"result": []}
    assert token_calls == 1
    assert api_calls == 2


def test_oauth_renews_once_after_api_401(oauth_environment: EnvironmentConfig) -> None:
    token_calls = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, api_calls
        if request.url.path == "/oauth_token.do":
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"access-{token_calls}",
                    "token_type": "bearer",
                    "expires_in": 3600,
                },
            )
        api_calls += 1
        if api_calls == 1:
            assert request.headers["Authorization"] == "Bearer access-1"
            return httpx.Response(401, json={"error": {"message": "expired"}})
        assert request.headers["Authorization"] == "Bearer access-2"
        return httpx.Response(200, json={"result": []})

    transport = httpx.MockTransport(handler)
    with TableApiClient(oauth_environment, transport=transport) as client:
        assert client.call("GET", "api/now/table/sys_user", retries=0) == {"result": []}
    assert token_calls == 2
    assert api_calls == 2


def test_oauth_refresh_token_rotation_is_saved_to_keychain(
    isolated_credential_store: credentials.MemoryBackend,
) -> None:
    url = "https://dev.service-now.com/"
    credentials.store_secret(url, "TEST_CLIENT_ID", "client-id")
    credentials.store_secret(url, "TEST_CLIENT_SECRET", "client-secret")
    credentials.store_secret(url, "TEST_REFRESH_TOKEN", "refresh-old")
    environment = EnvironmentConfig.model_validate(
        {
            "name": "dev",
            "url": url,
            "kind": "development",
            "auth": {
                "mode": "oauth",
                "oauth_grant_type": "refresh_token",
                "client_id_env": "TEST_CLIENT_ID",
                "client_secret_env": "TEST_CLIENT_SECRET",
                "refresh_token_env": "TEST_REFRESH_TOKEN",
                "store": "keychain",
            },
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth_token.do":
            assert parse_qs(request.content.decode())["refresh_token"] == ["refresh-old"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-new",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(200, json={"result": []})

    transport = httpx.MockTransport(handler)
    with TableApiClient(environment, transport=transport) as client:
        assert client.call("GET", "api/now/table/sys_user") == {"result": []}
    assert credentials.read_keychain(url, "TEST_REFRESH_TOKEN") == "refresh-new"


def test_oauth_errors_do_not_include_secrets(oauth_environment: EnvironmentConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error_description": "client-secret access-token refresh-token"},
        )

    transport = httpx.MockTransport(handler)
    with (
        TableApiClient(oauth_environment, transport=transport) as client,
        pytest.raises(ServiceNowError) as exc_info,
    ):
        client.call("GET", "api/now/table/sys_user")
    message = str(exc_info.value)
    assert "returned 400" in message
    assert "client-secret" not in message
    assert "access-token" not in message


def test_cicd_style_error_surfaces_the_real_status_message(
    environment: EnvironmentConfig,
) -> None:
    """sn_cicd (CI/CD API) failures are shaped like Table API ones: the message lives
    under "result.status_message", and "result.error" is often an empty string rather
    than absent, so it must not be mistaken for "no message"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "result": {
                    "status": "3",
                    "status_label": "Failed",
                    "status_message": "Missing parameter: sys_id or scope required",
                    "status_detail": "",
                    "error": "",
                }
            },
        )

    transport = httpx.MockTransport(handler)
    with (
        TableApiClient(environment, transport=transport) as client,
        pytest.raises(ServiceNowError) as exc_info,
    ):
        client.call("POST", "api/sn_cicd/update_set/create")
    assert "Missing parameter: sys_id or scope required" in str(exc_info.value)


def test_connect_error_retries_exhausted(environment: EnvironmentConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection failed")

    sleep_calls: list[int | float] = []
    transport = httpx.MockTransport(handler)
    with TableApiClient(environment, transport=transport, sleep=sleep_calls.append) as client:
        with pytest.raises(ServiceNowError) as exc_info:
            client.call("GET", "api/now/table/sys_user", retries=2)
        assert "Connection failed" in str(exc_info.value)
    assert sleep_calls == [1, 2]


def test_invalid_json_response_raises_error(environment: EnvironmentConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{invalid_json")

    transport = httpx.MockTransport(handler)
    with TableApiClient(environment, transport=transport) as client:
        with pytest.raises(ServiceNowError) as exc_info:
            client.call("GET", "api/now/table/sys_user")
        assert "returned invalid JSON" in str(exc_info.value)


def test_non_object_json_response_raises_error(environment: EnvironmentConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"sys_id": "123"}])

    transport = httpx.MockTransport(handler)
    with TableApiClient(environment, transport=transport) as client:
        with pytest.raises(ServiceNowError) as exc_info:
            client.call("GET", "api/now/table/sys_user")
        assert "returned a non-object response" in str(exc_info.value)
