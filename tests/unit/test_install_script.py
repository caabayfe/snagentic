from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging" / "install" / "install.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX installer"
)


def _release(directory: Path, version: str = "9.9.9", *, corrupt: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    bundle = directory / "build" / "snagentic"
    bundle.mkdir(parents=True)
    executable = bundle / "snagentic"
    executable.write_text(f"#!/bin/sh\necho snagentic {version}\n")
    executable.chmod(0o755)
    (bundle / "_internal").mkdir()
    archive = directory / f"snagentic-{version}-macos-arm64.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(bundle, arcname="snagentic")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if corrupt:
        digest = "0" * 64
    other = "1" * 64
    (directory / "SHA256SUMS").write_text(
        f"{other}  snagentic-{version}-windows-x64.zip\r\n{digest}  {archive.name}\r\n"
    )
    shutil.rmtree(directory / "build")
    return directory


def _install(tmp_path: Path, release: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "SNAGENTIC_DOWNLOAD_URL": str(release),
        "SNAGENTIC_TARGET": "macos-arm64",
        "SNAGENTIC_NO_COPILOT": "1",
    }
    return subprocess.run(
        ["sh", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def test_installs_and_upgrades_in_place(tmp_path: Path) -> None:
    result = _install(tmp_path, _release(tmp_path / "r1", "1.0.0"))
    assert result.returncode == 0, result.stderr
    link = tmp_path / "home" / ".local" / "bin" / "snagentic"
    app = tmp_path / "home" / ".local" / "share" / "snagentic"
    assert link.is_symlink() and link.resolve() == (app / "snagentic").resolve()
    assert "snagentic 1.0.0" in result.stdout

    result = _install(tmp_path, _release(tmp_path / "r2", "2.0.0"))
    assert result.returncode == 0, result.stderr
    assert subprocess.run([str(link)], capture_output=True, text=True).stdout.strip() == (
        "snagentic 2.0.0"
    )
    assert not (app.parent / "snagentic.new").exists()


def test_rejects_checksum_mismatch(tmp_path: Path) -> None:
    result = _install(tmp_path, _release(tmp_path / "r", corrupt=True))
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (tmp_path / "home" / ".local" / "share" / "snagentic").exists()


def test_rejects_missing_target(tmp_path: Path) -> None:
    release = _release(tmp_path / "r")
    (release / "SHA256SUMS").write_text("")
    result = _install(tmp_path, release)
    assert result.returncode != 0 and "no macos-arm64 archive" in result.stderr


def test_refuses_to_replace_unrelated_directory(tmp_path: Path) -> None:
    unrelated = tmp_path / "home" / ".local" / "share" / "snagentic"
    unrelated.mkdir(parents=True)
    (unrelated / "keep.txt").write_text("mine")
    result = _install(tmp_path, _release(tmp_path / "r"))
    assert result.returncode != 0 and "not a snagentic installation" in result.stderr
    assert (unrelated / "keep.txt").read_text() == "mine"
