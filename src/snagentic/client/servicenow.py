"""HTTP client for the snagentic ServiceNow companion API."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping
from hashlib import sha256
from typing import Any
from urllib.parse import urljoin

import httpx

from snagentic import __version__
from snagentic.artifacts.normalization import canonical_json
from snagentic.artifacts.registry import DEFAULT_ARTIFACT_REGISTRY
from snagentic.config import EnvironmentConfig, resolve_credentials
from snagentic.errors import ConfigurationError, ServiceNowError

API_PREFIX = "/api/x_snagentic_source/source/v1"


class ServiceNowClient:
    def __init__(self, config: EnvironmentConfig) -> None:
        self.config = config
        mode, credentials = resolve_credentials(config)
        headers = {"Accept": "application/json", "User-Agent": f"snagentic/{__version__}"}
        auth: httpx.Auth | None = None
        if mode == "bearer":
            headers["Authorization"] = f"Bearer {credentials}"
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
        )

    def __enter__(self) -> ServiceNowClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        url = urljoin(str(self._client.base_url), path.lstrip("/"))
        for attempt in range(retries + 1):
            try:
                response = self._client.request(method, url, params=params, json=json)
            except httpx.HTTPError as exc:
                if attempt == retries:
                    raise ServiceNowError(f"{method} {path} failed: {exc}") from exc
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code in {429, 502, 503, 504} and attempt < retries:
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else min(2**attempt, 8)
                )
                time.sleep(delay)
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                if response.is_error:
                    request_id = response.headers.get("X-Request-ID", "unknown")
                    raise ServiceNowError(
                        f"{method} {path} returned {response.status_code} "
                        f"(request {request_id})",
                        status_code=response.status_code,
                    ) from exc
                raise ServiceNowError(f"{method} {path} returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise ServiceNowError(f"{method} {path} returned a non-object response")
            if payload.get("ok") is False:
                error = payload.get("error")
                code = error.get("code") if isinstance(error, dict) else "unknown"
                details = error.get("details") if isinstance(error, dict) else []
                safe_details = (
                    [item for item in details if isinstance(item, dict)]
                    if isinstance(details, list)
                    else []
                )
                raise ServiceNowError(
                    f"{method} {path} was rejected ({code})",
                    status_code=response.status_code,
                    code=str(code),
                    details=safe_details,
                )
            if response.is_error:
                request_id = response.headers.get("X-Request-ID", "unknown")
                raise ServiceNowError(
                    f"{method} {path} returned {response.status_code} (request {request_id})",
                    status_code=response.status_code,
                )
            return payload
        raise ServiceNowError(f"{method} {path} exhausted its retry budget")

    def capabilities(self) -> dict[str, Any]:
        return _data_object(self.request("GET", f"{API_PREFIX}/capabilities"))

    def domains(self) -> list[dict[str, Any]]:
        domains: list[dict[str, Any]] = []
        for item in self._paged_items(f"{API_PREFIX}/domains"):
            parent = item.get("parent")
            parent_id = (
                parent.get("sys_id")
                if isinstance(parent, Mapping)
                and isinstance(parent.get("sys_id"), str)
                else None
            )
            domains.append(
                {
                    **item,
                    "stable_id": _required_string(item, "sys_id"),
                    "parent_stable_id": parent_id,
                }
            )
        return domains

    def contexts(self) -> list[dict[str, Any]]:
        return list(self._paged_items(f"{API_PREFIX}/contexts"))

    def inventory(self) -> Iterator[dict[str, Any]]:
        capabilities = self.capabilities()
        artifact_types = capabilities.get("artifact_types")
        if not isinstance(artifact_types, list):
            raise ServiceNowError("capabilities response is missing artifact_types")
        for context in self.contexts():
            domain = _nested_identifier(context, "domain", "sys_id")
            scope = _nested_identifier(context, "application_scope", "sys_id")
            for definition in artifact_types:
                if not isinstance(definition, dict):
                    raise ServiceNowError("artifact_types must contain objects")
                artifact_type = definition.get("key")
                if not isinstance(artifact_type, str) or not artifact_type:
                    raise ServiceNowError("artifact type key must be a non-empty string")
                yield from self._paged_items(
                    f"{API_PREFIX}/artifacts",
                    params={
                        "artifact_type": artifact_type,
                        "domain_id": domain,
                        "scope_id": scope,
                    },
                )

    def export_artifact(self, inventory_item: Mapping[str, Any]) -> dict[str, Any]:
        request_item = {
            "artifact_type": _required_string(inventory_item, "artifact_type"),
            "sys_id": _required_string(inventory_item, "sys_id"),
            "domain": {
                "sys_id": _nested_identifier(inventory_item, "domain", "sys_id")
            },
            "application_scope": {
                "sys_id": _nested_identifier(
                    inventory_item, "application_scope", "sys_id"
                )
            },
        }
        payload = self.request(
            "POST",
            f"{API_PREFIX}/artifacts/export",
            json={"artifacts": [request_item]},
        )
        artifacts = _object_list(_data_object(payload), "artifacts")
        if len(artifacts) != 1:
            raise ServiceNowError("artifact export did not return exactly one artifact")
        return _flatten_artifact(artifacts[0])

    def preflight(self, bundle: dict[str, Any]) -> dict[str, Any]:
        return _data_object(
            self.request("POST", f"{API_PREFIX}/preflight", json=bundle)
        )

    def apply_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        if not self.config.writable:
            raise ServiceNowError(f"writes are disabled for {self.config.kind} environments")
        return _data_object(
            self.request(
                "POST",
                f"{API_PREFIX}/change-bundles/apply",
                json=bundle,
                retries=0,
            )
        )

    def diagnostics(self, query: dict[str, Any]) -> dict[str, Any]:
        return _data_object(
            self.request("GET", f"{API_PREFIX}/diagnostics", params=query)
        )

    def _paged_items(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        next_cursor: str | None = None
        while True:
            page_params = dict(params or {})
            page_params["limit"] = min(self.config.page_size, 200)
            if next_cursor:
                page_params["cursor"] = next_cursor
            data = _data_object(self.request("GET", path, params=page_params))
            yield from _object_list(data, "items")
            raw_cursor = data.get("next_cursor")
            next_cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
            if next_cursor is None:
                break


def _object_list(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ServiceNowError(f"response field {key!r} must be a list of objects")
    return value


def _data_object(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("data")
    if not isinstance(value, dict):
        raise ServiceNowError("response field 'data' must be an object")
    return value


def _required_string(value: Mapping[str, Any], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise ServiceNowError(f"response field {key!r} must be a non-empty string")
    return candidate


def _nested_identifier(value: Mapping[str, Any], key: str, identifier: str) -> str:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ServiceNowError(f"response field {key!r} must be an object")
    return _required_string(nested, identifier)


def _flatten_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    values = artifact.get("values")
    if not isinstance(values, Mapping):
        raise ServiceNowError("artifact export values must be an object")
    domain = _nested_identifier(artifact, "domain", "sys_id")
    scope_object = artifact.get("application_scope")
    if not isinstance(scope_object, Mapping):
        raise ServiceNowError("artifact application_scope must be an object")
    scope = scope_object.get("scope") or scope_object.get("sys_id")
    if not isinstance(scope, str) or not scope:
        raise ServiceNowError("artifact application scope is missing its identifier")
    artifact_type = _required_string(artifact, "artifact_type")
    stable_key = _stable_natural_key(artifact_type, values, artifact)
    code_field = next(
        (
            field
            for field in ("script", "operation_script", "condition", "filter_condition")
            if isinstance(values.get(field), str)
        ),
        None,
    )
    flattened = dict(artifact)
    flattened.update(
        {
            "domain": domain,
            "scope": scope,
            "scope_sys_id": _required_string(scope_object, "sys_id"),
            "stable_key": stable_key,
            "table": artifact_type,
            "content": values[code_field] if code_field else dict(values),
        }
    )
    if code_field:
        flattened["extension"] = "js"
        flattened["code_field"] = code_field
        flattened["fields"] = {
            key: item for key, item in values.items() if key != code_field
        }
    flattened.pop("application_scope", None)
    flattened.pop("values", None)
    return flattened


def _stable_natural_key(
    artifact_type: str,
    values: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> str:
    definition = DEFAULT_ARTIFACT_REGISTRY.get(artifact_type)
    fields = definition.natural_key_fields if definition is not None else ("name",)
    components = [str(values[field]) for field in fields if values.get(field) not in (None, "")]
    if len(components) != len(fields):
        components = [_required_string(artifact, "sys_id")]
    digest = sha256(canonical_json(components).encode("utf-8")).hexdigest()[:12]
    label = "-".join(components)
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("._-")[:80] or artifact_type
    return f"{label}-{digest}"
