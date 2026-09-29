from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "render_cask", ROOT / "packaging" / "homebrew" / "render_cask.py")
assert spec is not None and spec.loader is not None
cask: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cask)

ARM, X64 = "a" * 64, "b" * 64


def _sums(tmp_path: Path, *arches: str, version: str = "1.2.3") -> Path:
    digests = {"arm64": ARM, "x64": X64}
    lines = [f"{digests[arch]}  snagentic-{version}-macos-{arch}.tar.gz" for arch in arches]
    lines.append(f"{'c' * 64}  snagentic-{version}-windows-x64.zip")
    path = tmp_path / "SHA256SUMS"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_release_cask_has_both_architectures_and_livecheck(tmp_path: Path) -> None:
    sums = cask.read_sums(_sums(tmp_path, "arm64", "x64"))
    text = cask.render(version="1.2.3", digests=cask.mac_digests(sums, "1.2.3", partial=False),
                       repository="acme/snagentic")
    assert 'arch arm: "arm64", intel: "x64"' in text
    assert f'sha256 arm:   "{ARM}",\n         intel: "{X64}"' in text
    assert ('url "https://github.com/acme/snagentic/releases/download/v#{version}/'
            'snagentic-#{version}-macos-#{arch}.tar.gz"') in text
    assert 'homepage "https://github.com/acme/snagentic"' in text
    assert "strategy :github_latest" in text and "depends_on macos: :ventura" in text
    assert 'binary "snagentic/snagentic"' in text and "snagentic copilot install" in text
    assert not re.search(r"\{\{[A-Z_]+\}\}", text) and "\n\n\n" not in text
    assert "postflight" not in text and not text.startswith("#")


def test_partial_cask_for_local_testing(tmp_path: Path) -> None:
    sums = cask.read_sums(_sums(tmp_path, "arm64"))
    with pytest.raises(cask.RenderError, match="x64"):
        cask.mac_digests(sums, "1.2.3", partial=False)
    digests = cask.mac_digests(sums, "1.2.3", partial=True)
    with pytest.raises(cask.RenderError, match="https"):
        cask.render(version="1.2.3", digests=digests, repository=None,
                    url_base="file:///tmp/dist", homepage="https://example.com")
    text = cask.render(version="1.2.3", digests=digests, repository=None,
                       url_base="file:///tmp/dist/", homepage="https://example.com",
                       allow_file_url=True)
    assert "arch arm:" not in text and f'sha256 "{ARM}"' in text
    assert 'url "file:///tmp/dist/snagentic-#{version}-macos-arm64.tar.gz"' in text
    assert "depends_on arch: :arm64" in text and "livecheck" not in text
    with pytest.raises(cask.RenderError, match="missing"):
        cask.mac_digests(sums, "9.9.9", partial=True)


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"version": "latest"}, "invalid version"),
    ({"repository": "not a repo"}, "invalid repository"),
    ({"repository": None}, "--repository or --url-base"),
    ({"repository": None, "url_base": "http://example.com"}, "https"),
    ({"repository": None, "url_base": 'https://x/"a'}, "not allowed"),
    ({"repository": None, "url_base": "https://artifacts.example.com"}, "--homepage"),
])
def test_render_rejects_bad_input(kwargs: dict[str, Any], message: str) -> None:
    arguments = {"version": "1.2.3", "digests": {"arm64": ARM, "x64": X64},
                 "repository": "acme/snagentic", **kwargs}
    with pytest.raises(cask.RenderError, match=message):
        cask.render(**arguments)


def test_internal_artifact_host(tmp_path: Path) -> None:
    text = cask.render(version="1.2.3", digests={"arm64": ARM, "x64": X64}, repository=None,
                       url_base="https://artifacts.example.com/snagentic",
                       homepage="https://docs.example.com/snagentic")
    assert 'url "https://artifacts.example.com/snagentic/snagentic-#{version}' in text
    assert "livecheck" not in text


def test_read_sums_accepts_directories_and_rejects_garbage(tmp_path: Path) -> None:
    (tmp_path / "a.tar.gz.sha256").write_text(f"{ARM}  snagentic-1.2.3-macos-arm64.tar.gz\n")
    (tmp_path / "b.tar.gz.sha256").write_text(f"{X64} *snagentic-1.2.3-macos-x64.tar.gz\n\n")
    assert cask.read_sums(tmp_path) == {"snagentic-1.2.3-macos-arm64.tar.gz": ARM,
                                        "snagentic-1.2.3-macos-x64.tar.gz": X64}
    (tmp_path / "empty").mkdir()
    with pytest.raises(cask.RenderError, match="no"):
        cask.read_sums(tmp_path / "empty")
    (tmp_path / "bad.sha256").write_text("not a checksum\n")
    with pytest.raises(cask.RenderError, match="sha256sum"):
        cask.read_sums(tmp_path)


def test_main_writes_the_cask_and_reports_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    sums = _sums(tmp_path, "arm64", "x64", version="1.2.3")
    output = tmp_path / "tap" / "Casks" / "snagentic.rb"
    assert cask.main(["--sums", str(sums), "--version", "v1.2.3",
                      "--repository", "acme/snagentic", "--output", str(output)]) == 0
    assert 'version "1.2.3"' in output.read_text()
    assert cask.main(["--sums", str(sums), "--version", "1.2.3",
                      "--repository", "acme/snagentic"]) == 0
    assert 'cask "snagentic" do' in capsys.readouterr().out
    assert cask.main(["--sums", str(sums), "--repository", "acme/snagentic"]) == 2
    assert "missing macOS archives" in capsys.readouterr().err
