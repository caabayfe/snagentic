"""ServiceNow credentials held in the operating system's secure credential store.

Configuration files only ever name a credential (for example ``SNAGENTIC_DEV_PASSWORD``).
The value lives in macOS Keychain, Windows Credential Manager or the Linux Secret
Service under the ``snagentic`` service with the account ``<host>/<name>``. Binding the
entry to the ServiceNow host means a configuration that points an existing credential
name at a different URL never receives that secret.

Only those native backends are used; keyring's plaintext or third-party backends are
never selected, so an unavailable store fails closed instead of degrading silently.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from snagentic.errors import ConfigurationError

SERVICE = "snagentic"
STORE_ENVIRONMENT_VARIABLE = "SNAGENTIC_CREDENTIAL_STORE"
CredentialStore = Literal["auto", "keychain", "env"]
STORES: tuple[CredentialStore, ...] = ("auto", "keychain", "env")


class CredentialStoreUnavailable(ConfigurationError):
    """The operating system credential store cannot be used on this machine."""


class SecretBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


_backend_override: SecretBackend | None = None


def set_backend(backend: SecretBackend | None) -> None:
    """Replace the native backend (tests and embedding only)."""

    global _backend_override
    _backend_override = backend


def backend() -> SecretBackend:
    if _backend_override is not None:
        return _backend_override
    try:
        # Typed as Any: the backend classes are untyped on some platforms.
        factory: Any
        if sys.platform == "darwin":
            from keyring.backends import macOS

            factory = macOS.Keyring
        elif sys.platform == "win32":
            from keyring.backends import Windows

            factory = Windows.WinVaultKeyring
        else:
            from keyring.backends import SecretService

            factory = SecretService.Keyring
        native = factory()
        # Each backend's priority property raises when it cannot run on this host.
        if float(native.priority) <= 0:
            raise RuntimeError("backend reported no priority")
    except Exception as exc:  # any failure means the store is unusable
        raise CredentialStoreUnavailable(
            f"the operating system credential store is unavailable: {exc}"
        ) from exc
    return cast(SecretBackend, native)


def backend_name() -> str:
    try:
        selected = backend()
    except CredentialStoreUnavailable:
        return "unavailable"
    return type(selected).__module__ + "." + type(selected).__name__


def host_key(url: object) -> str:
    parts = urlsplit(str(url))
    if not parts.hostname:
        raise ConfigurationError(f"credential URL has no host: {url}")
    host = parts.hostname.lower()
    return f"{host}:{parts.port}" if parts.port else host


def account(url: object, name: str) -> str:
    return f"{host_key(url)}/{name}"


def effective_store(
    configured: CredentialStore = "auto",
    environ: Mapping[str, str] | None = None,
) -> CredentialStore:
    """An explicit profile setting wins; ``auto`` may be narrowed by the environment.

    Native (frozen) builds treat ``auto`` as ``keychain`` so credentials are never
    read from process environments unless the user opts in.
    """

    if configured != "auto":
        return configured
    source = os.environ if environ is None else environ
    override = source.get(STORE_ENVIRONMENT_VARIABLE, "").strip().lower()
    if override:
        if override not in STORES:
            raise ConfigurationError(
                f"{STORE_ENVIRONMENT_VARIABLE} must be one of: {', '.join(STORES)}"
            )
        return override
    return "keychain" if getattr(sys, "frozen", False) else "auto"


def read_keychain(url: object, name: str) -> str | None:
    try:
        value = backend().get_password(SERVICE, account(url, name))
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable(
            f"could not read {name} from the credential store: {type(exc).__name__}"
        ) from exc
    return value or None


def resolve_secret(
    name: str,
    *,
    url: object,
    store: CredentialStore = "auto",
    environ: Mapping[str, str] | None = None,
) -> str | None:
    source = os.environ if environ is None else environ
    mode = effective_store(store, source)
    if mode in {"auto", "keychain"}:
        try:
            value = read_keychain(url, name)
        except CredentialStoreUnavailable:
            if mode == "keychain":
                raise
            value = None
        if value or mode == "keychain":
            return value
    return source.get(name) or None


def missing_message(names: list[str], *, url: object, store: CredentialStore,
                    environ: Mapping[str, str] | None = None) -> str:
    mode = effective_store(store, environ)
    joined = ", ".join(names)
    if mode == "env":
        return f"required environment variables are unset: {joined}"
    where = "the OS credential store" if mode == "keychain" else (
        "the OS credential store or the environment"
    )
    return (
        f"credentials not found in {where} for {host_key(url)}: {joined}; "
        "run `snagentic auth login` to store them"
    )


def store_secret(url: object, name: str, value: str) -> None:
    if not value:
        raise ConfigurationError(f"refusing to store an empty value for {name}")
    try:
        backend().set_password(SERVICE, account(url, name), value)
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable(
            f"could not store {name} in the credential store: {type(exc).__name__}"
        ) from exc


def delete_secret(url: object, name: str) -> bool:
    try:
        selected = backend()
        if selected.get_password(SERVICE, account(url, name)) is None:
            return False
        selected.delete_password(SERVICE, account(url, name))
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable(
            f"could not delete {name} from the credential store: {type(exc).__name__}"
        ) from exc
    return True


class MemoryBackend:
    """In-process backend for tests; never selected automatically."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)
