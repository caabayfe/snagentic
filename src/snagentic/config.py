"""Non-secret project and environment configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from snagentic.credentials import CredentialStore, missing_message, resolve_secret
from snagentic.errors import ConfigurationError


class AuthConfig(BaseModel):
    """How to authenticate. ``*_env`` fields name credentials, never hold values.

    ``store`` selects where the named values are read from: ``keychain`` (the OS
    credential store only), ``env`` (process environment only) or ``auto`` (keychain
    first, then the environment; native builds treat ``auto`` as ``keychain``).
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["bearer", "basic"] = "bearer"
    token_env: str | None = "SNAGENTIC_TOKEN"  # noqa: S105 - environment variable name
    username_env: str | None = None
    password_env: str | None = None
    store: CredentialStore = "auto"

    def credential_names(self) -> list[str]:
        names = [self.token_env] if self.mode == "bearer" else [
            self.username_env, self.password_env
        ]
        return [name for name in names if name]

    @model_validator(mode="after")
    def validate_mode_fields(self) -> AuthConfig:
        if self.mode == "bearer" and not self.token_env:
            raise ValueError("bearer authentication requires token_env")
        if self.mode == "basic" and (not self.username_env or not self.password_env):
            raise ValueError("basic authentication requires username_env and password_env")
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
) -> tuple[str, str | tuple[str, str]]:
    auth = config.auth

    def lookup(name: str) -> str | None:
        return resolve_secret(name, url=config.url, store=auth.store, environ=environ)

    if auth.mode == "bearer":
        if auth.token_env is None:
            raise ConfigurationError("bearer authentication requires token_env")
        token = lookup(auth.token_env)
        if not token:
            raise ConfigurationError(
                missing_message([auth.token_env], url=config.url, store=auth.store,
                                environ=environ)
            )
        return ("bearer", token)
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
