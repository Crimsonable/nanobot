#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-v0.29.0}"
INSTALL_DIR="${2:-$HOME/.local/bin}"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *)
    echo "Unsupported architecture: $ARCH" >&2
    exit 1
    ;;
esac

curl -fsSL -o /tmp/kind "https://kind.sigs.k8s.io/dl/${VERSION}/kind-linux-${ARCH}"
chmod +x /tmp/kind
mkdir -p "$INSTALL_DIR"
mv /tmp/kind "$INSTALL_DIR/kind"
"$INSTALL_DIR/kind" --version
