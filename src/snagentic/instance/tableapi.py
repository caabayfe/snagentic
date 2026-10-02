"""Read/write client for the standard ServiceNow Table and CI/CD REST APIs."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import httpx

from snagentic import __version__
from snagentic.config import EnvironmentConfig, OAuthCredentials, resolve_credentials
from snagentic.credentials import CredentialStoreUnavailable, effective_store, store_secret
from snagentic.errors import ConfigurationError, ServiceNowError

TABLE_PREFIX = "api/now/table/"
RETRY_STATUS = frozenset({429, 502, 503, 504})
TOKEN_EXPIRY_SKEW_SECONDS = 30.0


class OAuthTokenProvider:
    def __init__(
        self,
        config: EnvironmentConfig,
        credentials: OAuthCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
        clock: Any = time.monotonic,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self._sleep = sleep
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at: float | None = None
        self._refresh_token = credentials.refresh_token
        self._client = httpx.Client(
            base_url=str(config.url).rstrip("/") + "/",
            headers={
                "Accept": "application/json",
                "User-Agent": f"snagentic/{__version__}",
            },
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def token(self, *, force: bool = False) -> str:
        if (
            not force
            and self._access_token
            and (self._expires_at is None or self._clock() < self._expires_at)
        ):
            return self._access_token
        payload = {
            "grant_type": self.credentials.grant_type,
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
        }
        if self.credentials.grant_type == "refresh_token":
            if not self._refresh_token:
                raise ConfigurationError("OAuth refresh token is unavailable")
            payload["refresh_token"] = self._refresh_token
        response = self._request_token(payload)
        try:
            result = response.json()
        except ValueError as exc:
            raise ServiceNowError("OAuth token endpoint returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ServiceNowError("OAuth token endpoint returned a non-object response")
        access_token = result.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ServiceNowError("OAuth token endpoint response has no access_token")
        token_type = result.get("token_type")
        if token_type is not None and (
            not isinstance(token_type, str) or token_type.lower() != "bearer"
        ):
            raise ServiceNowError("OAuth token endpoint returned an unsupported token_type")
        self._access_token = access_token
        self._expires_at = _token_expiry(result.get("expires_in"), self._clock())
        self._update_refresh_token(result.get("refresh_token"))
        return access_token

    def _request_token(self, payload: Mapping[str, str]) -> httpx.Response:
        for attempt in range(4):
            try:
                response = self._client.post(self.credentials.token_endpoint, data=payload)
            except httpx.HTTPError as exc:
                if attempt >= 3:
                    raise ServiceNowError(f"OAuth token request failed: {exc}") from exc
                self._sleep(min(2**attempt, 8))
                continue
            if response.status_code in RETRY_STATUS and attempt < 3:
                retry_after = response.headers.get("Retry-After", "")
                self._sleep(float(retry_after) if retry_after.isdigit() else min(2**attempt, 8))
                continue
            if response.is_error:
                raise ServiceNowError(
                    f"OAuth token endpoint returned {response.status_code}",
                    status_code=response.status_code,
                )
            return response
        raise AssertionError("OAuth token retry loop exhausted")

    def _update_refresh_token(self, value: object) -> None:
        if (
            not isinstance(value, str)
            or not value
            or value == self._refresh_token
            or not self.credentials.refresh_token_name
        ):
            return
        self._refresh_token = value
        if self.credentials.store == "env":
            return
        try:
            store_secret(self.config.url, self.credentials.refresh_token_name, value)
        except CredentialStoreUnavailable:
            if effective_store(self.credentials.store) == "keychain":
                raise


def _token_expiry(value: object, now: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return now + max(0.0, seconds - TOKEN_EXPIRY_SKEW_SECONDS)


class TableApiClient:
    """Thin, retrying wrapper over ``/api/now/table``.

    Reads always request raw (non-display) values without reference links so the
    normalized mirror is deterministic and independent of the user's locale.
    """

    def __init__(
        self,
        config: EnvironmentConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.config = config
        self._sleep = sleep
        mode, credentials = resolve_credentials(config)
        self._oauth: OAuthTokenProvider | None = None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"snagentic/{__version__}",
        }
        auth: httpx.Auth | None = None
        if mode == "bearer":
            headers["Authorization"] = "Bearer " + str(credentials)
        elif mode == "oauth":
            if not isinstance(credentials, OAuthCredentials):
                raise ConfigurationError("OAuth credentials are incomplete")
            self._oauth = OAuthTokenProvider(config, credentials, transport=transport, sleep=sleep)
        else:
            if not isinstance(credentials, tuple):
                raise ConfigurationError("basic authentication credentials are incomplete")
            auth = httpx.BasicAuth(*credentials)
        self._client = httpx.Client(
            base_url=str(config.url).rstrip("/") + "/",
            headers=headers,
            auth=auth,
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            transport=transport,
        )

    def __enter__(self) -> TableApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        if self._oauth is not None:
            self._oauth.close()

    def call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        retries: int = 3,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        oauth_renewed = False
        attempt = 0
        while True:
            try:
                headers = (
                    {"Authorization": "Bearer " + self._oauth.token()}
                    if self._oauth is not None
                    else None
                )
                response = self._client.request(
                    method, path, params=params, json=json, headers=headers
                )
            except httpx.HTTPError as exc:
                if attempt >= retries:
                    raise ServiceNowError(f"{method} {path} failed: {exc}") from exc
                self._sleep(min(2**attempt, 8))
                attempt += 1
                continue
            if response.status_code == 401 and self._oauth is not None and not oauth_renewed:
                self._oauth.token(force=True)
                oauth_renewed = True
                continue
            if response.status_code in RETRY_STATUS and attempt < retries:
                retry_after = response.headers.get("Retry-After", "")
                self._sleep(float(retry_after) if retry_after.isdigit() else min(2**attempt, 8))
                attempt += 1
                continue
            if allow_missing and response.status_code in {400, 403, 404}:
                return None
            if response.status_code == 204:
                return {}
            if response.is_error:
                raise ServiceNowError(
                    f"{method} {path} returned {response.status_code}: {_error_message(response)}",
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ServiceNowError(f"{method} {path} returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise ServiceNowError(f"{method} {path} returned a non-object response")
            return payload
        raise ServiceNowError(f"{method} {path} exhausted its retry budget")

    def query(
        self,
        table: str,
        *,
        query: str = "",
        fields: Sequence[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        allow_missing: bool = False,
    ) -> list[dict[str, Any]] | None:
        params: dict[str, Any] = {
            "sysparm_query": query,
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true",
            "sysparm_limit": limit or self.config.page_size,
            "sysparm_offset": offset,
            "sysparm_no_count": "true",
        }
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        payload = self.call(
            "GET", TABLE_PREFIX + _safe_table(table), params=params, allow_missing=allow_missing
        )
        if payload is None:
            return None
        return _result_list(payload)

    def iterate(
        self,
        table: str,
        *,
        query: str = "",
        fields: Sequence[str] | None = None,
        allow_missing: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Offset paging for small, stable tables (catalog, scopes, dictionary)."""

        offset = 0
        size = self.config.page_size
        while True:
            page = self.query(
                table,
                query=query,
                fields=fields,
                limit=size,
                offset=offset,
                allow_missing=allow_missing,
            )
            # ACL-filtered rows are dropped after the limit applies: only an empty page ends.
            if not page:
                return
            yield from page
            offset += size

    def get(self, table: str, sys_id: str) -> dict[str, Any] | None:
        rows = self.query(table, query=f"sys_id={_safe_sys_id(sys_id)}", limit=1)
        return rows[0] if rows else None

    def insert(self, table: str, values: Mapping[str, Any]) -> dict[str, Any]:
        payload = self.call(
            "POST",
            TABLE_PREFIX + _safe_table(table),
            params={"sysparm_exclude_reference_link": "true"},
            json=dict(values),
            retries=0,
        )
        return _result_object(payload)

    def update(self, table: str, sys_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        payload = self.call(
            "PATCH",
            TABLE_PREFIX + f"{_safe_table(table)}/{_safe_sys_id(sys_id)}",
            params={"sysparm_exclude_reference_link": "true"},
            json=dict(values),
            retries=0,
        )
        return _result_object(payload)

    def delete(self, table: str, sys_id: str) -> None:
        self.call(
            "DELETE",
            TABLE_PREFIX + f"{_safe_table(table)}/{_safe_sys_id(sys_id)}",
            retries=0,
        )


def _result_list(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result")
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise ServiceNowError("Table API response field 'result' must be a list of objects")
    return result


def _result_object(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    result = (payload or {}).get("result")
    if not isinstance(result, dict):
        raise ServiceNowError("API response field 'result' must be an object")
    return result


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "no error body"
    if not isinstance(payload, dict):
        return "unknown error"
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail")
        if message:
            return str(message)[:300]
    # The sn_cicd (CI/CD) API reports failures inside "result" instead of the
    # Table API's top-level "error" object, e.g. {"result": {"status": "3",
    # "status_message": "Missing parameter: ...", "error": ""}}. "error" there is
    # often an empty string rather than absent, so check status_message first.
    result = payload.get("result")
    if isinstance(result, dict):
        message = (
            result.get("status_message")
            or (result.get("error") if isinstance(result.get("error"), str) else None)
            or result.get("error_message")
        )
        if message:
            return str(message)[:300]
    if isinstance(error, str) and error:
        return error[:300]
    return "unknown error"


def _safe_table(table: str) -> str:
    if not table or not all(ch.isalnum() or ch in "_$" for ch in table):
        raise ValueError(f"unsafe table name: {table!r}")
    return table


# Same contract as records.SYS_ID.
SYS_ID = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_ ]{0,62}[A-Za-z0-9_])?$")


def _safe_sys_id(sys_id: str) -> str:
    if not SYS_ID.fullmatch(sys_id):
        raise ValueError(f"invalid sys_id: {sys_id!r}")
    return sys_id
