from __future__ import annotations

import subprocess
from pathlib import Path

from snagentic.instance.config import InstancePaths
from snagentic.instance.mirror import (
    LARGE_REPOSITORY_SETTINGS,
    MirrorRepository,
    tune_large_repository,
    worktree_status,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "instances/dev/metadata/a").mkdir(parents=True)
    (tmp_path / "instances/dev/metadata/a/record.yaml").write_text("x: 1\n")
    (tmp_path / "other.txt").write_text("o\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_worktree_status_filters_whole_repository_output(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "instances/dev/metadata/a/record.yaml").write_text("x: 2\n")
    spaced = root / "instances/dev/metadata/default-view--Default view"
    spaced.mkdir()
    (spaced / "record.yaml").write_text("é: 1\n")
    (root / "other.txt").write_text("changed\n")
    (root / "instances/dev/metadata-other.txt").write_text("not under the prefix\n")

    result = worktree_status(root, ("instances/dev/metadata",))

    assert sorted(result) == sorted([
        ("??", "instances/dev/metadata/default-view--Default view/record.yaml"),
        (" M", "instances/dev/metadata/a/record.yaml"),
    ])


def test_tune_large_repository_respects_existing_settings(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "config", "core.untrackedCache", "false")
    tune_large_repository(root)
    assert _git(root, "config", "--local", "--get", "core.untrackedCache").strip() == "false"
    for key, value in LARGE_REPOSITORY_SETTINGS[1:]:
        assert _git(root, "config", "--local", "--get", key).strip() == value


def test_tune_large_repository_enables_supported_settings(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    tune_large_repository(root)

    for key, value in LARGE_REPOSITORY_SETTINGS:
        assert _git(root, "config", "--local", "--get", key).strip() == value


def test_mirror_construction_does_not_mutate_git_configuration(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    MirrorRepository(InstancePaths(root, "dev"), "servicenow-remote/dev")

    for key, _value in LARGE_REPOSITORY_SETTINGS:
        result = subprocess.run(
            ["git", "config", "--local", "--get", key],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
