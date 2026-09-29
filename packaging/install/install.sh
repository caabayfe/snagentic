#!/bin/sh
# Install or upgrade snagentic on macOS without Homebrew, signing or admin rights.
#
#   curl -fsSL https://github.com/caabayfe/snagentic/releases/latest/download/install.sh | sh
#
# Files downloaded with curl carry no quarantine attribute, so Gatekeeper does not block
# the unsigned bundle. The archive is verified against the release SHA256SUMS first.
#
# Settings (environment variables, all optional):
#   SNAGENTIC_VERSION       release to install, e.g. 0.2.0 (default: latest)
#   SNAGENTIC_REPO          GitHub repository (default: caabayfe/snagentic)
#   SNAGENTIC_DOWNLOAD_URL  base URL or local directory holding the archives and SHA256SUMS
#   SNAGENTIC_HOME          application directory (default: ~/.local/share/snagentic)
#   SNAGENTIC_BIN_DIR       directory for the `snagentic` link (default: ~/.local/bin)
#   SNAGENTIC_NO_COPILOT=1  skip `snagentic copilot install`
set -eu

fail() { echo "snagentic install: $*" >&2; exit 1; }

repo="${SNAGENTIC_REPO:-caabayfe/snagentic}"
version="${SNAGENTIC_VERSION:-latest}"
app_dir="${SNAGENTIC_HOME:-$HOME/.local/share/snagentic}"
bin_dir="${SNAGENTIC_BIN_DIR:-$HOME/.local/bin}"

if [ -z "${SNAGENTIC_TARGET:-}" ]; then
  case "$(uname -s)" in
    Darwin) os=macos ;;
    *) fail "unsupported system $(uname -s); on Windows use install.ps1" ;;
  esac
  case "$(uname -m)" in
    arm64 | aarch64) arch=arm64 ;;
    x86_64) arch=x64 ;;
    *) fail "unsupported architecture $(uname -m)" ;;
  esac
  target="$os-$arch"
else
  target="$SNAGENTIC_TARGET"
fi

if [ -n "${SNAGENTIC_DOWNLOAD_URL:-}" ]; then
  base="${SNAGENTIC_DOWNLOAD_URL%/}"
elif [ "$version" = latest ]; then
  base="https://github.com/$repo/releases/latest/download"
else
  base="https://github.com/$repo/releases/download/v${version#v}"
fi

case "$app_dir" in
  "" | / | "$HOME" | "$HOME/") fail "refusing to use $app_dir as the application directory" ;;
esac

fetch() {
  case "$base" in
    /*) cp "$base/$1" "$2" ;;
    *) curl -fsSL --retry 3 -o "$2" "$base/$1" ;;
  esac
}

sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else sha256sum "$1" | cut -d' ' -f1
  fi
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT INT TERM

fetch SHA256SUMS "$work/SHA256SUMS" || fail "cannot download SHA256SUMS from $base"
line="$(tr -d '\r' < "$work/SHA256SUMS" | grep -E "  snagentic-[^ ]+-$target\.tar\.gz\$" | head -n 1 || true)"
[ -n "$line" ] || fail "no $target archive in the release"
expected="${line%% *}"
archive="${line##* }"

echo "Downloading $archive"
fetch "$archive" "$work/$archive" || fail "cannot download $archive"
actual="$(sha256 "$work/$archive")"
[ "$actual" = "$expected" ] || fail "checksum mismatch for $archive"

mkdir -p "$work/extract"
tar -xzf "$work/$archive" -C "$work/extract"
[ -x "$work/extract/snagentic/snagentic" ] || fail "$archive does not contain snagentic/snagentic"
xattr -dr com.apple.quarantine "$work/extract/snagentic" 2>/dev/null || true

if [ -e "$app_dir" ] && [ ! -x "$app_dir/snagentic" ]; then
  fail "$app_dir exists and is not a snagentic installation"
fi
mkdir -p "$(dirname "$app_dir")" "$bin_dir"
rm -rf "$app_dir.new"
mv "$work/extract/snagentic" "$app_dir.new"
rm -rf "$app_dir"
mv "$app_dir.new" "$app_dir"
ln -sf "$app_dir/snagentic" "$bin_dir/snagentic"

"$bin_dir/snagentic" --version
if [ "${SNAGENTIC_NO_COPILOT:-0}" != 1 ]; then
  "$bin_dir/snagentic" copilot install >/dev/null && echo "Copilot CLI extension and plugin installed"
fi

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "Add $bin_dir to PATH, for example: echo 'export PATH=\"$bin_dir:\$PATH\"' >> ~/.zshrc" ;;
esac
echo "Next: snagentic auth login -i <instance>   (and optionally: snagentic ui install)"
