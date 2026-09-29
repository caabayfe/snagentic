import pytest
from pydantic import ValidationError

from snagentic.config import AuthConfig, EnvironmentConfig, resolve_credentials
from snagentic.errors import ConfigurationError


def test_environment_policy_is_development_only() -> None:
    dev = EnvironmentConfig(
        name="dev",
        url="https://dev.service-now.com/",
        kind="development",
    )
    prod = EnvironmentConfig(
        name="prod",
        url="https://prod.service-now.com/",
        kind="production",
    )
    assert dev.writable
    assert not prod.writable


def test_basic_credentials_are_resolved_from_named_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = EnvironmentConfig(
        name="dev",
        url="https://dev.service-now.com/",
        kind="development",
        auth=AuthConfig(
            mode="basic",
            username_env="SNAGENTIC_DEV_USERNAME",
            password_env="SNAGENTIC_DEV_PASSWORD",
        ),
    )
    monkeypatch.setenv("SNAGENTIC_DEV_USERNAME", "integration-user")
    monkeypatch.setenv("SNAGENTIC_DEV_PASSWORD", "integration-password")
    assert resolve_credentials(config) == ("basic", ("integration-user", "integration-password"))


def test_auth_config_rejects_incomplete_basic_credentials() -> None:
    with pytest.raises(ValidationError, match="username_env and password_env"):
        AuthConfig(mode="basic", username_env="SNAGENTIC_DEV_USERNAME")


def test_missing_bearer_secret_names_the_configured_variable() -> None:
    config = EnvironmentConfig(
        name="dev",
        url="https://dev.service-now.com/",
        kind="development",
        auth=AuthConfig(mode="bearer", token_env="SNAGENTIC_DEV_TOKEN", store="env"),
    )
    with pytest.raises(ConfigurationError, match="SNAGENTIC_DEV_TOKEN"):
        resolve_credentials(config, environ={})
