"""Non-secret project and environment configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from snagentic.credentials import CredentialStore, missing_message, resolve_secret
from snagentic.errors import ConfigurationError


@dataclass(frozen=True, repr=False)
class OAuthCredentials:
    grant_type: Literal["client_credentials", "refresh_token"]
    client_id: str
    client_secret: str
    refresh_token: str | None
    refresh_token_name: str | None
    token_endpoint: str
    store: CredentialStore


class AuthConfig(BaseModel):
    """How to authenticate. ``*_env`` fields name credentials, never hold values.

    ``store`` selects where the named values are read from: ``keychain`` (the OS
    credential store only), ``env`` (process environment only) or ``auto`` (keychain
    first, then the environment; native builds treat ``auto`` as ``keychain``).
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["bearer", "basic", "oauth"] = "bearer"
    token_env: str | None = "SNAGENTIC_TOKEN"  # noqa: S105 - environment variable name
    username_env: str | None = None
    password_env: str | None = None
    oauth_grant_type: Literal["client_credentials", "refresh_token"] = "client_credentials"
    client_id_env: str | None = None
    client_secret_env: str | None = None
    refresh_token_env: str | None = None
    token_endpoint: str = "oauth_token.do"  # noqa: S105 - endpoint path, not a secret
    store: CredentialStore = "auto"

    def credential_names(self) -> list[str]:
        if self.mode == "bearer":
            names = [self.token_env]
        elif self.mode == "basic":
            names = [self.username_env, self.password_env]
        else:
            names = [self.client_id_env, self.client_secret_env]
            if self.oauth_grant_type == "refresh_token":
                names.append(self.refresh_token_env)
        return [name for name in names if name]

    @field_validator("token_endpoint")
    @classmethod
    def validate_token_endpoint(cls, value: str) -> str:
        endpoint = value.strip()
        parts = urlsplit(endpoint)
        if (
            not endpoint
            or parts.scheme
            or parts.netloc
            or parts.query
            or parts.fragment
            or any(part == ".." for part in parts.path.split("/"))
        ):
            raise ValueError("token_endpoint must be a same-instance relative path")
        return endpoint.lstrip("/")

    @model_validator(mode="after")
    def validate_mode_fields(self) -> AuthConfig:
        if self.mode == "bearer" and not self.token_env:
            raise ValueError("bearer authentication requires token_env")
        if self.mode == "basic" and (not self.username_env or not self.password_env):
            raise ValueError("basic authentication requires username_env and password_env")
        if self.mode == "oauth" and (not self.client_id_env or not self.client_secret_env):
            raise ValueError("OAuth authentication requires client_id_env and client_secret_env")
        if (
            self.mode == "oauth"
            and self.oauth_grant_type == "refresh_token"
            and not self.refresh_token_env
        ):
            raise ValueError("OAuth refresh_token authentication requires refresh_token_env")
        return self


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: HttpUrl
    kind: Literal["development", "test", "production"]
    auth: AuthConfig = Field(default_factory=AuthConfig)
    verify_tls: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    page_size: int = Field(default=500, ge=1, le=10_000)

    @property
    def writable(self) -> bool:
        return self.kind == "development"


def resolve_credentials(
    config: EnvironmentConfig,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str | tuple[str, str] | OAuthCredentials]:
    auth = config.auth

    def lookup(name: str) -> str | None:
        return resolve_secret(name, url=config.url, store=auth.store, environ=environ)

    if auth.mode == "bearer":
        if auth.token_env is None:
            raise ConfigurationError("bearer authentication requires token_env")
        token = lookup(auth.token_env)
        if not token:
            raise ConfigurationError(
                missing_message([auth.token_env], url=config.url, store=auth.store, environ=environ)
            )
        return ("bearer", token)
    if auth.mode == "oauth":
        if auth.client_id_env is None or auth.client_secret_env is None:
            raise ConfigurationError(
                "OAuth authentication requires client_id_env and client_secret_env"
            )
        client_id = lookup(auth.client_id_env)
        client_secret = lookup(auth.client_secret_env)
        refresh_token = (
            lookup(auth.refresh_token_env)
            if auth.oauth_grant_type == "refresh_token" and auth.refresh_token_env
            else None
        )
        required = [
            (auth.client_id_env, client_id),
            (auth.client_secret_env, client_secret),
        ]
        if auth.oauth_grant_type == "refresh_token" and auth.refresh_token_env:
            required.append((auth.refresh_token_env, refresh_token))
        missing = [name for name, value in required if not value]
        if missing:
            raise ConfigurationError(
                missing_message(missing, url=config.url, store=auth.store, environ=environ)
            )
        return (
            "oauth",
            OAuthCredentials(
                grant_type=auth.oauth_grant_type,
                client_id=client_id or "",
                client_secret=client_secret or "",
                refresh_token=refresh_token,
                refresh_token_name=auth.refresh_token_env,
                token_endpoint=auth.token_endpoint,
                store=auth.store,
            ),
        )
    if auth.username_env is None or auth.password_env is None:
        raise ConfigurationError("basic authentication requires username_env and password_env")
    username = lookup(auth.username_env)
    password = lookup(auth.password_env)
    missing = [
        name
        for name, value in (
            (auth.username_env, username),
            (auth.password_env, password),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(
            missing_message(missing, url=config.url, store=auth.store, environ=environ)
        )
    return ("basic", (username or "", password or ""))
