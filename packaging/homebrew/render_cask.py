"""Render the snagentic Homebrew cask from a release's SHA-256 checksums.

Usage::

    python packaging/homebrew/render_cask.py --sums SHA256SUMS \\
        --repository OWNER/REPO --output Casks/snagentic.rb

``--sums`` accepts a ``SHA256SUMS`` file or a directory of ``*.sha256`` files, as written
by ``packaging/build_native.py``. Release casks need both macOS archives (arm64 and x64).
``--partial`` renders a single-architecture cask for local testing, and ``--url-base``
serves the archives from somewhere other than GitHub releases (an internal artifact
host, or ``file://`` with ``--allow-file-url`` for local tests).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "snagentic.rb"
ARCHES = {"arm64": "arm", "x64": "intel"}
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?$")
REPOSITORY = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
SUM_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})\s+\*?(?P<name>\S+)$")
TOKEN = re.compile(r"\{\{[A-Z0-9_]+\}\}")


class RenderError(ValueError):
    pass


def read_sums(path: Path) -> dict[str, str]:
    files = sorted(path.glob("*.sha256")) if path.is_dir() else [path]
    if not files:
        raise RenderError(f"no *.sha256 files in {path}")
    sums: dict[str, str] = {}
    for file in files:
        for line in file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            match = SUM_LINE.match(line.strip())
            if match is None:
                raise RenderError(f"{file}: not a sha256sum line: {line!r}")
            sums[match["name"]] = match["digest"]
    return sums


def mac_digests(sums: dict[str, str], version: str, *, partial: bool) -> dict[str, str]:
    digests = {arch: sums[name] for arch in ARCHES
               if (name := f"snagentic-{version}-macos-{arch}.tar.gz") in sums}
    missing = sorted(set(ARCHES) - set(digests))
    if not digests or (missing and not partial):
        raise RenderError(f"checksums for snagentic {version} are missing macOS archives: "
                          + ", ".join(missing))
    return digests


def render(*, version: str, digests: dict[str, str], repository: str | None,
           url_base: str | None = None, homepage: str | None = None,
           allow_file_url: bool = False) -> str:
    if not VERSION.fullmatch(version):
        raise RenderError(f"invalid version {version!r}")
    if repository is not None and not REPOSITORY.fullmatch(repository):
        raise RenderError(f"invalid repository {repository!r}; expected OWNER/REPO")
    if url_base is None:
        if repository is None:
            raise RenderError("give --repository or --url-base")
        url_base = f"https://github.com/{repository}/releases/download/v#{{version}}"
    elif not (url_base.startswith("https://")
              or (allow_file_url and url_base.startswith("file:///"))):
        raise RenderError("--url-base must be an https:// URL")
    if any(char in url_base for char in "\"\\\n") or "{{" in url_base:
        raise RenderError("--url-base contains characters that are not allowed")
    homepage = homepage or (f"https://github.com/{repository}" if repository else None)
    if homepage is None or not homepage.startswith("https://") or '"' in homepage:
        raise RenderError("give an https:// --homepage when not publishing to GitHub")

    both = len(digests) == len(ARCHES)
    if both:
        arch = '  arch arm: "arm64", intel: "x64"\n'
        sha256 = (f'  sha256 arm:   "{digests["arm64"]}",\n'
                  f'         intel: "{digests["x64"]}"\n')
        archive = "snagentic-#{version}-macos-#{arch}.tar.gz"
        depends = "  depends_on macos: :ventura"
    else:
        (only, digest), = digests.items()
        arch = ""
        sha256 = f'  sha256 "{digest}"\n'
        archive = f"snagentic-#{{version}}-macos-{only}.tar.gz"
        depends = (f"  depends_on arch: :{'arm64' if only == 'arm64' else 'x86_64'}\n"
                   "  depends_on macos: :ventura")
    livecheck = ("\n  livecheck do\n    url :url\n    strategy :github_latest\n  end\n"
                 if repository and url_base.startswith("https://github.com/") else "")
    text = TEMPLATE.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if not line.startswith("# "))
    values = {
        "{{ARCH}}": arch.rstrip("\n") + ("\n" if arch else ""),
        "{{VERSION}}": version,
        "{{SHA256}}": sha256 + "\n",
        "{{URL}}": f"{url_base.rstrip('/')}/{archive}",
        "{{HOMEPAGE}}": homepage,
        "{{LIVECHECK}}": livecheck,
        "{{DEPENDS_ON}}": depends,
    }
    for token, value in values.items():
        text = text.replace(token, value)
    if TOKEN.search(text):
        raise RenderError(f"unrendered template token {TOKEN.search(text)[0]}")  # type: ignore[index]
    text = re.sub(r"\n{3,}", "\n\n", text).replace("do\n\n", "do\n")
    return text.lstrip("\n") + ("" if text.endswith("\n") else "\n")


def _version() -> str:
    namespace: dict[str, str] = {}
    init = HERE.parents[1] / "src" / "snagentic" / "__init__.py"
    exec(init.read_text(encoding="utf-8"), namespace)  # noqa: S102
    return namespace["__version__"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sums", type=Path, required=True)
    parser.add_argument("--version", help="release version (default: the package version)")
    parser.add_argument("--repository", help="GitHub OWNER/REPO that hosts the releases")
    parser.add_argument("--url-base", help="base URL of the archives instead of GitHub")
    parser.add_argument("--homepage")
    parser.add_argument("--partial", action="store_true",
                        help="allow a single-architecture cask (local testing)")
    parser.add_argument("--allow-file-url", action="store_true")
    parser.add_argument("--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)
    version = (args.version or _version()).removeprefix("v")
    try:
        cask = render(
            version=version,
            digests=mac_digests(read_sums(args.sums), version, partial=args.partial),
            repository=args.repository, url_base=args.url_base, homepage=args.homepage,
            allow_file_url=args.allow_file_url,
        )
    except RenderError as exc:
        print(f"render_cask: {exc}", file=sys.stderr)
        return 2
    if args.output is None:
        sys.stdout.write(cask)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(cask, encoding="utf-8")
        print(f"cask: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
