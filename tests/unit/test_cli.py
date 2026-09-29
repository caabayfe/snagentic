import argparse
from pathlib import Path

from snagentic.cli.main import run


def test_init_creates_non_secret_template(tmp_path: Path, monkeypatch: object) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(
        command="init",
        config=Path("config/snagentic.yaml"),
        auth="bearer",
    )
    result = run(args)
    content = (tmp_path / "config/snagentic.yaml").read_text(encoding="utf-8")
    assert result["created"] is True
    assert "SNAGENTIC_DEV_TOKEN" in content
    assert "token:" not in content


def test_init_can_create_basic_auth_template(
    tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(
        command="init",
        config=Path("config/snagentic.yaml"),
        auth="basic",
    )
    result = run(args)
    content = (tmp_path / "config/snagentic.yaml").read_text(encoding="utf-8")
    assert result["created"] is True
    assert "mode: basic" in content
    assert "SNAGENTIC_DEV_USERNAME" in content
    assert "SNAGENTIC_DEV_PASSWORD" in content
    assert "service-account-name" not in content
