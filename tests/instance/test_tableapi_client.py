from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from snagentic import __version__
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
