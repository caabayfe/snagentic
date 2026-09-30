import pytest
from pydantic import ValidationError

from snagentic.config import AuthConfig, EnvironmentConfig, OAuthCredentials, resolve_credentials
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


def test_oauth_client_credentials_are_resolved_without_exposing_values() -> None:
    config = EnvironmentConfig(
        name="dev",
        url="https://dev.service-now.com/",
        kind="development",
        auth=AuthConfig(
            mode="oauth",
            client_id_env="SNAGENTIC_DEV_CLIENT_ID",
            client_secret_env="SNAGENTIC_DEV_CLIENT_SECRET",
            store="env",
        ),
    )
    mode, resolved = resolve_credentials(
        config,
        environ={
            "SNAGENTIC_DEV_CLIENT_ID": "client-id",
            "SNAGENTIC_DEV_CLIENT_SECRET": "client-secret",
        },
    )
    assert mode == "oauth"
    assert isinstance(resolved, OAuthCredentials)
    assert resolved.grant_type == "client_credentials"
    assert resolved.client_id == "client-id"
    assert resolved.client_secret == "client-secret"
    assert "client-secret" not in repr(resolved)


def test_oauth_refresh_token_requires_all_credential_names() -> None:
    with pytest.raises(ValidationError, match="refresh_token_env"):
        AuthConfig(
            mode="oauth",
            oauth_grant_type="refresh_token",
            client_id_env="SNAGENTIC_DEV_CLIENT_ID",
            client_secret_env="SNAGENTIC_DEV_CLIENT_SECRET",
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://identity.example.com/oauth/token",
        "//identity.example.com/oauth/token",
        "../oauth_token.do",
        "oauth_token.do?audience=service-now",
    ],
)
def test_oauth_token_endpoint_must_stay_on_the_instance(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="same-instance relative path"):
        AuthConfig(
            mode="oauth",
            client_id_env="SNAGENTIC_DEV_CLIENT_ID",
            client_secret_env="SNAGENTIC_DEV_CLIENT_SECRET",
            token_endpoint=endpoint,
        )
