from __future__ import annotations

import hashlib
import importlib.util
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load() -> object:
    spec = importlib.util.spec_from_file_location(
        "build_native", ROOT / "packaging" / "build_native.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load()


def test_version_matches_package() -> None:
    from snagentic import __version__

    assert build.version() == __version__  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("platform_name", "machine", "expected"),
    [("darwin", "arm64", "macos-arm64"), ("darwin", "x86_64", "macos-x64"),
     ("win32", "AMD64", "windows-x64")],
)
def test_target_name(monkeypatch: pytest.MonkeyPatch, platform_name: str, machine: str,
                     expected: str) -> None:
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(build.platform, "machine", lambda: machine)  # type: ignore[attr-defined]
    assert build.target_name() == expected  # type: ignore[attr-defined]


@pytest.mark.parametrize("platform_name", ["darwin", "win32"])
def test_archive_writes_checksum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 platform_name: str) -> None:
    bundle = tmp_path / "snagentic"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "snagentic").write_text("binary")
    (bundle / "_internal" / "data.txt").write_text("data")
    monkeypatch.setattr(sys, "platform", platform_name)
    archive = build.archive(bundle, tmp_path, "snagentic-0-test")  # type: ignore[attr-defined]
    checksum = (tmp_path / f"{archive.name}.sha256").read_text().split()
    assert checksum == [hashlib.sha256(archive.read_bytes()).hexdigest(), archive.name]
    if platform_name == "win32":
        with zipfile.ZipFile(archive) as handle:
            names = handle.namelist()
    else:
        with tarfile.open(archive) as handle:
            names = handle.getnames()
    assert "snagentic/_internal/data.txt" in names


def test_stage_extension_copies_only_modules(tmp_path: Path) -> None:
    staged = build.stage_extension(tmp_path)  # type: ignore[attr-defined]
    assert sorted(path.name for path in staged.iterdir()) == [
        "extension.mjs", "instance.mjs", "lib.mjs",
    ]
