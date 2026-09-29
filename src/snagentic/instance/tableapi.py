"""Read/write client for the standard ServiceNow Table and CI/CD REST APIs."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import httpx

from snagentic import __version__
from snagentic.config import EnvironmentConfig, resolve_credentials
from snagentic.errors import ConfigurationError, ServiceNowError

TABLE_PREFIX = "api/now/table/"
RETRY_STATUS = frozenset({429, 502, 503, 504})


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
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"snagentic/{__version__}",
        }
        auth: httpx.Auth | None = None
        if mode == "bearer":
            headers["Authorization"] = "Bearer " + str(credentials)
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
        for attempt in range(retries + 1):
            try:
                response = self._client.request(method, path, params=params, json=json)
            except httpx.HTTPError as exc:
                if attempt >= retries:
                    raise ServiceNowError(f"{method} {path} failed: {exc}") from exc
                self._sleep(min(2**attempt, 8))
                continue
            if response.status_code in RETRY_STATUS and attempt < retries:
                retry_after = response.headers.get("Retry-After", "")
                self._sleep(float(retry_after) if retry_after.isdigit() else min(2**attempt, 8))
                continue
            if allow_missing and response.status_code in {400, 403, 404}:
                return None
            if response.status_code == 204:
                return {}
            if response.is_error:
                raise ServiceNowError(
                    f"{method} {path} returned {response.status_code}: "
                    f"{_error_message(response)}",
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
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("detail") or "unknown error")[:300]
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
