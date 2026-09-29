from collections.abc import Iterator

import pytest

from snagentic import credentials


@pytest.fixture(autouse=True)
def isolated_credential_store(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[credentials.MemoryBackend]:
    """Never touch the developer's real keychain; tests opt into stored values explicitly."""

    store = credentials.MemoryBackend()
    credentials.set_backend(store)
    monkeypatch.delenv(credentials.STORE_ENVIRONMENT_VARIABLE, raising=False)
    yield store
    credentials.set_backend(None)


@pytest.fixture(autouse=True)
def no_copilot_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never register plugins with the developer's real Copilot CLI."""

    from snagentic.cli import native

    monkeypatch.setattr(native, "COPILOT_RUNNER", lambda argv, environ: None)
