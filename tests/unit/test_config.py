from pathlib import Path

import pytest

from snagentic.config import load_config, resolve_credentials
from snagentic.errors import ConfigurationError


def test_load_config_and_environment_policy(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
default_environment: dev
environments:
  dev:
    name: dev
    url: https://dev.service-now.com/
    kind: development
    auth:
      mode: bearer
      token_env: TEST_TOKEN
  prod:
    name: prod
    url: https://prod.service-now.com/
    kind: production
    auth:
      mode: bearer
      token_env: TEST_TOKEN
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.environment(None).writable
    assert not config.environment("prod").writable


def test_unknown_environment_is_explicit(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
default_environment: dev
environments:
  dev:
    name: dev
    url: https://dev.service-now.com/
    kind: development
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unknown environment"):
        load_config(path).environment("missing")


def test_workspace_paths_cannot_escape_repository(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
default_environment: dev
workspace: ../outside
environments:
  dev:
    name: dev
    url: https://dev.service-now.com/
    kind: development
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="repository-relative"):
        load_config(path)


def test_basic_credentials_are_resolved_from_named_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
default_environment: dev
environments:
  dev:
    name: dev
    url: https://dev.service-now.com/
    kind: development
    auth:
      mode: basic
      username_env: SNAGENTIC_DEV_USERNAME
      password_env: SNAGENTIC_DEV_PASSWORD
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SNAGENTIC_DEV_USERNAME", "integration-user")
    monkeypatch.setenv("SNAGENTIC_DEV_PASSWORD", "integration-password")
    assert resolve_credentials(load_config(path).environment("dev")) == (
        "basic",
        ("integration-user", "integration-password"),
    )
