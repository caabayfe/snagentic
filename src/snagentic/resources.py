"""Locations of bundled assets and per-user runtime directories.

Assets are found in this order:

1. a native (PyInstaller) bundle: ``<bundle>/snagentic_assets/<name>``;
2. an installed wheel: ``snagentic/_assets/<name>``;
3. a source checkout: the repository paths listed in ``CHECKOUT_ASSETS``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

PROTOCOL_VERSION = 1
PACKAGE_ROOT = Path(__file__).resolve().parent
CHECKOUT_ROOT = PACKAGE_ROOT.parents[1]
CHECKOUT_ASSETS = {
    "copilot-extension": Path(".github/extensions/snagentic"),
    "ui": Path("ui"),
    "copilot-plugin": Path("copilot-plugin"),
}
ASSET_MARKERS = {"copilot-extension": "extension.mjs", "ui": "recipes",
                 "copilot-plugin": "plugin.json"}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if is_frozen() and base else None


def asset_candidates(name: str) -> list[Path]:
    if name not in CHECKOUT_ASSETS:
        raise KeyError(name)
    candidates: list[Path] = []
    bundle = bundle_root()
    if bundle is not None:
        candidates.append(bundle / "snagentic_assets" / name)
    candidates.append(PACKAGE_ROOT / "_assets" / name)
    candidates.append(CHECKOUT_ROOT / CHECKOUT_ASSETS[name])
    return candidates


def asset_dir(name: str) -> Path | None:
    for candidate in asset_candidates(name):
        if (candidate / ASSET_MARKERS[name]).exists():
            return candidate
    return None


def bundled_node() -> Path | None:
    bundle = bundle_root()
    if bundle is None:
        return None
    executable = bundle / "runtime" / ("node.exe" if sys.platform == "win32" else "node")
    return executable if executable.is_file() else None


def _home(environ: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environ is None else environ
    home = source.get("HOME") or source.get("USERPROFILE")
    return Path(home) if home else Path.home()


def user_cache_dir(environ: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environ is None else environ
    if sys.platform == "win32":
        base = source.get("LOCALAPPDATA")
        return (Path(base) if base else _home(source) / "AppData" / "Local") / "snagentic" / "Cache"
    if sys.platform == "darwin":
        return _home(source) / "Library" / "Caches" / "snagentic"
    base = source.get("XDG_CACHE_HOME")
    return (Path(base) if base else _home(source) / ".cache") / "snagentic"


def user_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environ is None else environ
    if sys.platform == "win32":
        base = source.get("LOCALAPPDATA")
        return (Path(base) if base else _home(source) / "AppData" / "Local") / "snagentic"
    if sys.platform == "darwin":
        return _home(source) / "Library" / "Application Support" / "snagentic"
    base = source.get("XDG_DATA_HOME")
    return (Path(base) if base else _home(source) / ".local" / "share") / "snagentic"


def playwright_browsers_dir(environ: Mapping[str, str] | None = None) -> Path:
    return user_cache_dir(environ) / "ms-playwright"


def copilot_home(environ: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environ is None else environ
    configured = source.get("COPILOT_HOME")
    return Path(configured) if configured else _home(source) / ".copilot"
