from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from fake_servicenow import FakeServiceNow

from snagentic.instance.config import (
    DEFAULT_BASELINE_EXCLUDE,
    InstanceConfig,
    InstancePaths,
    InstanceRegistry,
)
from snagentic.instance.sync import InstanceSync
from snagentic.instance.tableapi import TableApiClient


def run_git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


@dataclass
class Harness:
    root: Path
    fake: FakeServiceNow
    config: InstanceConfig
    paths: InstancePaths
    client: TableApiClient

    def sync(self) -> InstanceSync:
        return InstanceSync(self.paths, self.config, self.client)

    def git(self, *args: str) -> str:
        return run_git(self.root, *args)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.name", "Test User")
    run_git(root, "config", "user.email", "test@example.com")
    (root / ".gitignore").write_text(".snagentic/\n")
    registry = InstanceRegistry(root)
    registry.add("dev", url="https://dev.example.service-now.com/", kind="development")
    # Keep the fake's table definitions (sys_db_object) out of record counts; the
    # "every class" coverage itself is exercised in test_sync.
    profile = registry.paths("dev").config_file
    raw = yaml.safe_load(profile.read_text(encoding="utf-8"))
    raw.setdefault("sync", {})["baseline_exclude"] = [*DEFAULT_BASELINE_EXCLUDE, "sys_db_object"]
    profile.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    monkeypatch.setenv("SNAGENTIC_DEV_USERNAME", "integration")
    monkeypatch.setenv("SNAGENTIC_DEV_PASSWORD", "not-a-real-password")
    monkeypatch.chdir(root)
    fake = FakeServiceNow()
    config = registry.load("dev")
    client = TableApiClient(config.environment(), transport=fake.transport(), sleep=lambda _: None)
    yield Harness(root, fake, config, registry.paths("dev"), client)
    client.close()
