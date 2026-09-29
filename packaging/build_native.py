"""Build the native, Docker-free snagentic application bundle.

The bundle is a PyInstaller one-directory application containing:

* the snagentic CLI and its Python runtime;
* ``snagentic_assets/copilot-extension`` - the user-scope Copilot CLI extension;
* ``snagentic_assets/copilot-plugin`` - the ServiceNow expert plugin (agents, skills, hook);
* ``snagentic_assets/ui`` - the Playwright UI runner with production node_modules;
* ``runtime/node[.exe]`` - a private Node.js runtime for the UI runner.

Playwright browsers are not bundled; ``snagentic ui install`` downloads them into the
per-user cache. Usage::

    python packaging/build_native.py [--node /path/to/node] [--skip-ui] [--output dist]

Signing and notarization happen afterwards in the release workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / ".github" / "extensions" / "snagentic"
PLUGIN = ROOT / "copilot-plugin"
UI = ROOT / "ui"
UI_FILES = ("package.json", "package-lock.json")
UI_DIRS = ("src", "recipes")
NOTICES = ("LICENSE", "SECURITY.md")

HIDDEN_IMPORTS = {
    "darwin": ["keyring.backends.macOS"],
    "win32": ["keyring.backends.Windows", "win32ctypes.core"],
}


def target_name() -> str:
    system = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine, machine
    )
    return f"{system}-{arch}"


def version() -> str:
    namespace: dict[str, str] = {}
    exec((ROOT / "src" / "snagentic" / "__init__.py").read_text(encoding="utf-8"), namespace)  # noqa: S102
    return namespace["__version__"]


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)  # noqa: S603


def resolve_node(explicit: str | None) -> Path:
    candidate = explicit or shutil.which("node")
    if not candidate:
        raise SystemExit("node was not found; pass --node /path/to/node")
    node = Path(candidate).resolve()
    if not node.is_file():
        raise SystemExit(f"node executable not found: {node}")
    return node


def npm_command(node: Path) -> list[str]:
    for candidate in (
        node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
        node.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
    ):
        if candidate.is_file():
            return [str(node), str(candidate)]
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm was not found next to node or on PATH")
    return [npm]


def stage_ui(stage: Path, node: Path, *, install: bool) -> Path:
    destination = stage / "ui"
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    for name in UI_FILES:
        shutil.copy2(UI / name, destination / name)
    for name in UI_DIRS:
        shutil.copytree(UI / name, destination / name)
    if install:
        env = dict(os.environ, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1")
        run([*npm_command(node), "ci", "--omit=dev", "--no-audit", "--no-fund"], destination, env)
    return destination


def stage_extension(stage: Path) -> Path:
    destination = stage / "copilot-extension"
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    for source in sorted(EXTENSION.glob("*.mjs")):
        shutil.copy2(source, destination / source.name)
    return destination


def pyinstaller(stage: Path, work: Path, dist: Path, node: Path, ui: Path, extension: Path) -> Path:
    separator = os.pathsep
    arguments = [
        str(ROOT / "packaging" / "snagentic_entry.py"),
        "--name", "snagentic",
        "--onedir",
        "--console",
        "--noconfirm",
        "--clean",
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(stage),
        "--paths", str(ROOT / "src"),
        "--collect-submodules", "snagentic",
        "--copy-metadata", "keyring",
        "--add-data", f"{extension}{separator}snagentic_assets/copilot-extension",
        "--add-data", f"{ui}{separator}snagentic_assets/ui",
        "--add-data", f"{PLUGIN}{separator}snagentic_assets/copilot-plugin",
        "--add-binary", f"{node}{separator}runtime",
    ]
    for module in HIDDEN_IMPORTS.get(sys.platform, ["keyring.backends.SecretService"]):
        arguments += ["--hidden-import", module]
    for notice in NOTICES:
        if (ROOT / notice).is_file():
            arguments += ["--add-data", f"{ROOT / notice}{separator}."]
    run([sys.executable, "-m", "PyInstaller", *arguments], ROOT)
    return dist / "snagentic"


def archive(bundle: Path, output: Path, name: str) -> Path:
    if sys.platform == "win32":
        path = output / f"{name}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as handle:
            for file in sorted(bundle.rglob("*")):
                handle.write(file, Path("snagentic") / file.relative_to(bundle))
    else:
        path = output / f"{name}.tar.gz"
        with tarfile.open(path, "w:gz") as handle:
            handle.add(bundle, arcname="snagentic")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # Bytes, not text: Windows text mode would write CRLF and break `shasum -c`.
    (output / f"{path.name}.sha256").write_bytes(f"{digest}  {path.name}\n".encode())
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--node", help="Node.js executable to bundle (default: node on PATH)")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "native")
    parser.add_argument("--skip-ui-install", action="store_true",
                        help="bundle UI sources without node_modules (development only)")
    parser.add_argument("--no-archive", action="store_true")
    args = parser.parse_args(argv)

    node = resolve_node(args.node)
    output = args.output.resolve()
    build = ROOT / "build" / "native"
    stage = build / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    ui = stage_ui(stage, node, install=not args.skip_ui_install)
    extension = stage_extension(stage)
    bundle = pyinstaller(stage, build / "work", output, node, ui, extension)
    print(f"bundle: {bundle}")
    if not args.no_archive:
        name = f"snagentic-{version()}-{target_name()}"
        print(f"archive: {archive(bundle, output, name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
