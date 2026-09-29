from __future__ import annotations

import sys
from pathlib import Path

import pytest

from snagentic import credentials
from snagentic.config import AuthConfig, EnvironmentConfig, resolve_credentials
from snagentic.errors import ConfigurationError

URL = "https://dev.service-now.com/"


def environment(**auth: str) -> EnvironmentConfig:
    fields = {"mode": "basic", "username_env": "SNAGENTIC_DEV_USERNAME",
              "password_env": "SNAGENTIC_DEV_PASSWORD", **auth}
    return EnvironmentConfig(name="dev", url=URL, kind="development",
                             auth=AuthConfig.model_validate(fields))


def test_keychain_values_are_bound_to_the_instance_host(
    isolated_credential_store: credentials.MemoryBackend,
) -> None:
    credentials.store_secret(URL, "SNAGENTIC_DEV_PASSWORD", "stored")
    assert isolated_credential_store.values == {
        ("snagentic", "dev.service-now.com/SNAGENTIC_DEV_PASSWORD"): "stored"
    }
    assert credentials.resolve_secret("SNAGENTIC_DEV_PASSWORD", url=URL, environ={}) == "stored"
    # A profile pointing the same credential name at another host never receives it.
    assert credentials.resolve_secret(
        "SNAGENTIC_DEV_PASSWORD", url="https://attacker.example.com/", environ={}
    ) is None
    assert credentials.account("https://Dev.Example.com:8443/x", "N") == "dev.example.com:8443/N"


def test_resolution_order_and_store_modes() -> None:
    credentials.store_secret(URL, "SNAGENTIC_DEV_USERNAME", "from-keychain")
    environ = {"SNAGENTIC_DEV_USERNAME": "from-env", "SNAGENTIC_DEV_PASSWORD": "env-pw"}
    assert resolve_credentials(environment(), environ) == ("basic", ("from-keychain", "env-pw"))
    assert resolve_credentials(environment(store="env"), environ) == (
        "basic", ("from-env", "env-pw"))
    with pytest.raises(ConfigurationError, match="OS credential store for dev.service-now.com"):
        resolve_credentials(environment(store="keychain"), environ)
    with pytest.raises(ConfigurationError, match="required environment variables are unset"):
        resolve_credentials(environment(store="env"), {})


def test_environment_override_only_narrows_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    assert credentials.effective_store("auto", {}) == "auto"
    assert credentials.effective_store("auto", {"SNAGENTIC_CREDENTIAL_STORE": "keychain"}) == (
        "keychain")
    assert credentials.effective_store("keychain", {"SNAGENTIC_CREDENTIAL_STORE": "env"}) == (
        "keychain")
    with pytest.raises(ConfigurationError):
        credentials.effective_store("auto", {"SNAGENTIC_CREDENTIAL_STORE": "plaintext"})
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert credentials.effective_store("auto", {}) == "keychain"


def test_native_builds_never_fall_back_to_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    environ = {"SNAGENTIC_DEV_USERNAME": "u", "SNAGENTIC_DEV_PASSWORD": "p"}
    with pytest.raises(ConfigurationError, match="snagentic auth login"):
        resolve_credentials(environment(), environ)


class BrokenBackend(credentials.MemoryBackend):
    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError("locked")


def test_unavailable_store_fails_closed_for_keychain_and_falls_back_for_auto() -> None:
    credentials.set_backend(BrokenBackend())
    environ = {"SNAGENTIC_DEV_USERNAME": "u", "SNAGENTIC_DEV_PASSWORD": "p"}
    assert resolve_credentials(environment(), environ) == ("basic", ("u", "p"))
    with pytest.raises(credentials.CredentialStoreUnavailable, match="RuntimeError"):
        resolve_credentials(environment(store="keychain"), environ)


def test_native_backend_selection_rejects_unusable_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials.set_backend(None)
    monkeypatch.setattr(sys, "platform", "unknown-os")

    import keyring.backends.SecretService as secret_service

    class Unavailable:
        @property
        def priority(self) -> float:
            raise RuntimeError("no D-Bus session")

    monkeypatch.setattr(secret_service, "Keyring", Unavailable)
    with pytest.raises(credentials.CredentialStoreUnavailable, match="no D-Bus session"):
        credentials.backend()
    assert credentials.backend_name() == "unavailable"


def test_store_and_delete_validate_values(
    isolated_credential_store: credentials.MemoryBackend,
) -> None:
    with pytest.raises(ConfigurationError, match="empty"):
        credentials.store_secret(URL, "SNAGENTIC_DEV_TOKEN", "")
    credentials.store_secret(URL, "SNAGENTIC_DEV_TOKEN", "t")
    assert credentials.delete_secret(URL, "SNAGENTIC_DEV_TOKEN") is True
    assert credentials.delete_secret(URL, "SNAGENTIC_DEV_TOKEN") is False
    with pytest.raises(ConfigurationError, match="no host"):
        credentials.host_key("not-a-url")


def test_example_configuration_documents_the_store_setting() -> None:
    root = Path(__file__).resolve().parents[2]
    assert "store:" in (root / "config" / "instance.example.yaml").read_text()
