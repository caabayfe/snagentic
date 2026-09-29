from __future__ import annotations

import subprocess
from pathlib import Path

from snagentic.instance.config import InstancePaths
from snagentic.instance.mirror import MirrorRepository


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def test_ensure_restores_incomplete_private_workspace(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    record = tmp_path / "instances/dev/metadata/a/record.yaml"
    record.parent.mkdir(parents=True)
    record.write_text("x: 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    branch = "servicenow-remote/dev"
    _git(tmp_path, "branch", branch)
    paths = InstancePaths(root=tmp_path, name="dev")
    mirror = MirrorRepository(paths, branch)
    assert mirror.restore() is True
    private_record = paths.mirror_tree / "instances/dev/metadata/a/record.yaml"
    private_record.unlink()

    assert mirror.ensure() is True
    assert private_record.read_text() == "x: 1\n"
